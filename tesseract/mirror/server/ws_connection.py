"""WS connection lifecycle — handshake, background pumps, teardown, autosave."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from tesseract.paths import TESSERACT_HOME, log_dir
from tesseract.memory.log_notes import append_log_entry
from tesseract.mirror.server.envelope import (
    make_cost_state,
    make_entity_signals,
    make_envelope,
    make_voice_instruction,
)
from tesseract.mirror.server import chat_store, session_autosave, spawn_wake
from tesseract.mirror.server.chat_lifecycle import _open_chats_payload
from tesseract.mirror.server.cors import origin_is_allowed
from tesseract.mirror.server.session import (
    ChatInfraNotReady,
    ServerSession,
    cleanup_session,
    create_server_session,
    send_envelope,
)
from tesseract.mirror.server.voice_io import note_voice_audio

log = logging.getLogger(__name__)

#: The background-bus channels this socket forwards verbatim, and the session
#: slot each pump's task lives in. Adding a channel is one row: the pump, the
#: spawn and the cancel all read this.
_FORWARDED_CHANNELS: tuple[tuple[str, str], ...] = (
    ("surface_events_task", "surface"),
    ("activity_events_task", "activity"),
    ("panel_events_task", "panel"),
)

# Frontend `IntensitySignals.BACKEND_STALENESS_MS = 3000`. Pumping every 2.0s
# keeps the freshness window with a 1s jitter margin.
ENTITY_SIGNALS_PUMP_INTERVAL_S = 2.0


def _spawn_tracked(app: web.Application, coro, name: str) -> asyncio.Task:
    """Route a fire-and-forget task through `scheduler.spawn_tracked_task` so
    engine shutdown can join/cancel it cleanly. Falls back to a bare
    `asyncio.create_task` when the scheduler hasn't started (tests, partial
    boot) — accepts the leak risk during the bootstrap window in exchange
    for not coupling these spawns to scheduler readiness.
    """
    scheduler = app.get("scheduler")
    if scheduler is not None:
        return scheduler.spawn_tracked_task(coro, name=name)
    return asyncio.create_task(coro, name=name)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    # Refuse before the upgrade: `/ws` dispatches `terminal_*` straight to the
    # PTY, which spawns a configured shell without a permission prompt. Binding
    # to loopback is no defence — a page in the operator's browser can reach
    # 127.0.0.1, and browsers do not apply same-origin policy to WS handshakes.
    origin = request.headers.get("Origin", "")
    if not origin_is_allowed(origin, request.app["allowed_origins"]):
        log.warning("ws: refused handshake from origin %r", origin)
        raise web.HTTPForbidden(text="origin not allowed")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    try:
        session = create_server_session(request.app, ws)
    except ChatInfraNotReady:
        # Boot race — the WS listener accepts connections before
        # `_build_chat_infra` sets `adapter_entry` (~20-30s at boot). Close
        # cleanly with TRY_AGAIN_LATER so the frontend's reconnect loop retries
        # once chat infra is ready, instead of crashing the handler.
        log.info("ws: chat infra not ready yet — asking client to reconnect")
        await ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"chat infra booting")
        return ws
    # Paired with the closing line in `finally`. Building a session is not free
    # (adapter chain, tool registry, chat restore) and tearing one down persists
    # and re-indexes every open chat, so a connection that cycles costs both
    # ends repeatedly. A teardown line on its own reads identically whether the
    # session lived six seconds or six hours, and says nothing about who ended
    # it.
    opened_at = time.monotonic()
    # `getattr`, because a line whose only job is to be readable later must not
    # be able to end the connection it is describing.
    log.info(
        "ws: session %s open (origin=%s, peer=%s)",
        session.session_id, origin or "(none)", getattr(request, "remote", None) or "?",
    )
    # Any operator WS connection counts as a renderer for
    # agent-spawned PTY panes. Previously `primary_ws` was only set when
    # the operator dispatched a `terminal_*` message (i.e. while on the
    # Terminal tab); an agent-spawned viewer pane (`start_controller_session`
    # / boot-time reattach) would then refuse with `no_primary_ws` if the
    # operator was on Chat / Workspace / any other tab. Promote on connect
    # so the viewer-pane open works whenever a Mirror tab is open.
    # `pty_manager.cleanup_for_ws` clears the ref on disconnect.
    request.app["primary_ws"] = ws
    # The try starts HERE, not at the receive loop. Everything below can
    # raise (a client that vanishes during the handshake makes the first
    # `send_envelope` fail), and four background pumps are already spawned
    # by the end of it. Opening the block at the loop instead leaves that
    # failure with an open log line and no close, and leaks every pump it
    # started.
    try:
        await send_envelope(session, make_envelope(
            "session_created",
            "session",
            session.session_id,
            {
                "session_id": session.session_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                # The frontend seeds its active chat
                # slice from this so the default slice key matches the backend's
                # chat_id; turn-scoped envelopes then route by exact id.
                "active_chat_id": session.active_chat_id,
                # P3 — the open-chat list (newest-first, with titles) so the tab
                # strip rehydrates on (re)connect and survives a page reload.
                "chats": _open_chats_payload(session),
            },
        ))
        await _announce_replaced_config(session)
        # The measured shape of this conversation, sent once on connect. It
        # used to ride only on a finished turn, so a surface that reads it
        # (the compaction bar) had nothing to draw until the operator
        # happened to run one. A panel that is blank until you poke something
        # unrelated reads as broken, and it was.
        from tesseract.mirror.server.turn_runner import emit_stats

        await emit_stats(request.app, session, chat_id=session.active_chat_id)
        await _emit_cost_state(request.app, session)
        await _emit_entity_signals(request.app, session)
        await _flush_stt_fallback_notice(request.app, session)
        session.entity_signals_task = _spawn_tracked(
            request.app,
            _entity_signals_pump(request.app, session),
            f"entity_signals_pump:{session.session_id}",
        )
        # Surface Protocol events, Unified Activity deltas, and the nudge that
        # tells a settings panel one of its cached fetches is out of date. All
        # three are operator-global: a tool call in any session lights up the
        # cockpit in front of the operator.
        for attribute, channel in _FORWARDED_CHANNELS:
            setattr(
                session,
                attribute,
                _spawn_tracked(
                    request.app,
                    _channel_forward_pump(session, channel),
                    f"{channel}_events_pump:{session.session_id}",
                ),
            )
        session.autosave_task = _spawn_tracked(
            request.app,
            session_autosave.autosave_pump(request.app, session),
            f"autosave_pump:{session.session_id}",
        )
        # Spawn push Stage 2 — wrap every open chat's spawn completion notifier so a
        # background spawn finishing while that chat is idle starts a proactive turn.
        spawn_wake.install(request.app, session)
        # A result that landed while the backend was down was replayed into its
        # rebuilt chat at restore. Wake that chat now rather than making the
        # operator speak first — same reason a live completion wakes an idle chat.
        spawn_wake.reconcile_on_connect(request.app, session)
        # Lazy: `_dispatch` stays in ws.py (the slim router), which re-exports
        # this module's public names — a module-level import here would cycle
        # with that re-export.
        from tesseract.mirror.server import ws as _ws
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _ws._dispatch(request.app, session, msg.data)
            elif msg.type == WSMsgType.BINARY:
                # Buffering, wake decoding and the voice loop, in the order
                # they have to happen — one call, because splitting them is
                # how the frame reached the accumulator but not the decoder.
                await note_voice_audio(
                    request.app,
                    session,
                    msg.data,
                    turn_active=session.current_turn_task is not None,
                )
            elif msg.type == WSMsgType.ERROR:
                log.warning("ws error: %s", ws.exception())
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING):
                break
    finally:
        # `getattr` for the same reason the open line uses it: this runs in a
        # `finally` that owns every teardown step below it, so a diagnostic
        # that raised here would skip the cancels and the save.
        log.info(
            "ws: session %s closing after %.1fs (close_code=%s, turns=%d)",
            session.session_id,
            time.monotonic() - opened_at,
            getattr(ws, "close_code", None),
            getattr(session, "turn_count", 0),
        )
        await _cancel_entity_signals_pump(session)
        for attribute, channel in _FORWARDED_CHANNELS:
            await _cancel_pump(session, attribute, channel)
        # Before the teardown save below: both write the same files, and the
        # last word must be the complete save.
        await _cancel_autosave_pump(session)
        try:
            await request.app["pty_manager"].cleanup_for_ws(ws)
        finally:
            try:
                await _autosave(request.app, session)
            finally:
                cleanup_session(request.app, session)
    return ws


async def _announce_replaced_config(session: ServerSession) -> None:
    """Tell the operator this release replaced their config, once.

    Boot is where the replacement happens and there is no WS open then, so the
    notice waits here for the first connection, and firing it stamps
    `announced` on the marker — an operator told on every launch that their
    settings were replaced learns to dismiss the message, which is the one
    message that must not become furniture.

    The marker itself SURVIVES. A toast is gone in seconds and the operator
    may have several panes of settings to put back; the Workspace notice reads
    the same file and stays until they dismiss it.

    Rides the existing `config_reloaded` envelope rather than inventing one:
    the file really did change under the running process, so bumping the
    frontend's reload counter and refetching the dependent panels is exactly
    right.
    """
    from tesseract.config_seed import config_replaced_marker_path
    from tesseract.mirror.server.envelope import make_config_reloaded

    marker = config_replaced_marker_path()
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    files = [str(name) for name in (payload.get("files") or [])]
    if not files or payload.get("announced"):
        return
    backup_dir = str(payload.get("backup_dir") or "")
    try:
        await send_envelope(session, make_config_reloaded(
            session.session_id,
            file="Settings",
            summary=(
                f"this update replaced {len(files)} config file"
                f"{'' if len(files) == 1 else 's'} — your previous copy is in "
                f"{backup_dir}"
            ),
            detail={"files": files, "backup_dir": backup_dir},
            ok=True,
        ))
    except Exception:
        log.exception("could not announce the config replacement")
        return
    try:
        marker.write_text(
            json.dumps({**payload, "announced": True}, indent=2), encoding="utf-8"
        )
    except OSError:
        log.exception("could not stamp the config-replaced marker")


async def _flush_stt_fallback_notice(app: web.Application, session: ServerSession) -> None:
    """Surface a latched local-STT failure as a toast on connect.

    A boot-time whisper warmup failure latches a one-shot notice on the
    engine (stt.py::warm_up_local), but its only consumer was the next
    *voice* commit — the operator never saw the problem unless they spoke
    (found live 2026-07-30: local STT dead all evening, zero UI signal).
    `voice_instruction` with `instruction` set already renders a warning
    toast on the frontend."""
    engine = app.get("stt_engine")
    if engine is None or not hasattr(engine, "consume_fallback_notice"):
        return
    try:
        notice = engine.consume_fallback_notice()
    except Exception:
        log.exception("stt fallback notice flush failed")
        return
    if notice:
        await send_envelope(
            session, make_voice_instruction(session.session_id, instruction=notice)
        )


async def _emit_cost_state(app: web.Application, session: ServerSession) -> None:
    """Send a `cost_state` catch-up envelope on WS connect so the HUD chips
    show today's spend immediately. Without this the chips read persisted
    localStorage (stale after midnight rollover) or stay empty until the
    next billed turn — both confuse the operator into thinking billing is
    broken. When the ledger is unavailable we surface that loudly via a
    `stream_error` envelope (toast) AND a server WARN so the operator
    sees why the Settings panel and HUD chips stay empty — silent no-op
    was the actual bug behind audit-3 follow-up: cost_state never fires,
    Settings → Voice stays "(loading… connect WS to load voice
    providers)" forever, and TTS bypasses budget enforcement."""
    ledger = app.get("cost_ledger")
    if ledger is None:
        log.warning(
            "cost_state: app['cost_ledger'] is None for session %s — "
            "check startup log for 'cost_ledger unavailable'. HUD chips will "
            "render empty/disabled, Settings → Voice will show a disabled state, "
            "and voice budgets are NOT enforced.",
            session.session_id,
        )
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {
                "message": "Cost ledger unavailable — HUD chips empty and voice budgets unenforced. Check server startup log.",
                "severity": "warning",
            },
        ))
        # Emit a synthetic disabled-state snapshot so the frontend exits its
        # "loading…" state and renders an empty/disabled UI rather than
        # appearing stuck. Without this, Settings → Voice sits on
        # "(loading… connect WS to load voice providers)" indefinitely
        # because `voiceProviders` stays null. The toast above explains
        # why; this envelope unsticks the panel.
        await send_envelope(session, make_cost_state(session.session_id, {
            "global": {
                "spent_usd": 0.0,
                "warning_usd": 0.0,
                "cap_usd": 0.0,
                "warning": False,
                "blocked": False,
            },
            "roles": {},
            "voice_providers": {"tts": {}, "stt": {}},
            "local_date": "",
            "enabled": False,
            "overage_unlocked": [],
            "warned": [],
        }))
        return
    try:
        snapshot = ledger.snapshot()
    except Exception:
        log.exception("cost_state snapshot failed for %s", session.session_id)
        return
    env = make_cost_state(session.session_id, snapshot)
    await send_envelope(session, env)


async def _emit_entity_signals(app: web.Application, session: ServerSession) -> None:
    mood = app.get("mood")
    if mood is None:
        return
    opts = app.get("adapter_options")
    effort = 1.0 if (opts is not None and getattr(opts, "tier", "api") == "cli") else 0.5
    env = make_entity_signals(
        session.session_id,
        mood_intensity=mood.intensity,
        mood_valence=mood.valence,
        effort_level=effort,
    )
    await send_envelope(session, env)


async def _entity_signals_pump(app: web.Application, session: ServerSession) -> None:
    try:
        while not session.ws.closed:
            await asyncio.sleep(ENTITY_SIGNALS_PUMP_INTERVAL_S)
            if session.ws.closed:
                return
            try:
                await _emit_entity_signals(app, session)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Per-iteration guard so a transient error in one emit (e.g.,
                # a TypeError reading mood, a socket-level surprise) cannot
                # kill the pump for the rest of the WS lifetime — that would
                # silently freeze frontend mood without any operator signal.
                log.exception("entity_signals emit failed for %s; continuing pump", session.session_id)
    except asyncio.CancelledError:
        raise


async def _cancel_entity_signals_pump(session: ServerSession) -> None:
    """Named rather than inlined: the teardown order is what it is called at."""
    await _cancel_pump(session, "entity_signals_task", "entity_signals")


async def _channel_forward_pump(session: ServerSession, channel: str) -> None:
    """Forward every background-bus envelope on ``channel`` to this socket.

    One pump for three channels, because the three differed only in the string
    they filtered on and a fourth copy is how the next one drifts.

    **Replay is dropped, deliberately.** Each of these channels has a REST
    catch-up path the frontend already calls on mount, so replaying the ring
    buffer could re-insert a surface the hydrate had settled, or announce a
    staleness the panel has already answered. What this carries is live only.
    """
    from tesseract.orchestrator.background_event_bus import get_background_bus

    bus = get_background_bus()
    _replay, queue = bus.subscribe()
    try:
        while not session.ws.closed:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                raise
            if not _envelope_for_channel(event.data, channel):
                continue
            if session.ws.closed:
                return
            try:
                await send_envelope(session, event.data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "%s forward failed for %s", channel, session.session_id
                )
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(queue)


async def _cancel_pump(
    session: ServerSession, attribute: str, label: str
) -> None:
    """Cancel one of this session's pumps and clear its slot."""
    task = getattr(session, attribute, None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("%s pump exit raised for %s", label, session.session_id)
    setattr(session, attribute, None)


async def _cancel_autosave_pump(session: ServerSession) -> None:
    """Named for the same reason, and this one carries a rule with it.

    It has to run BEFORE the teardown save, because both write the same files
    and the last word must be the complete save. A test hooks this name to
    prove that ordering, which is why it stays a function rather than becoming
    a fourth row in `_FORWARDED_CHANNELS`.
    """
    await _cancel_pump(session, "autosave_task", "autosave")


def _envelope_for_channel(payload: Any, channel: str) -> bool:
    if not isinstance(payload, dict):
        return False
    return payload.get("channel") == channel


def _session_chat_summary(session: ServerSession) -> tuple[int, int]:
    """Return ``(chat_count, total_turns)`` across ALL chats in the session.

    The legacy close-log counted only the active chat's turns; with multi-chat
    that under-reports a session that ran background chats. Turns = user turns
    (`ChatSession.turn_count`) summed over every open + archived chat.
    """
    chats = getattr(session, "chats", None) or {}
    total_turns = sum(cs.turn_count() for cs in chats.values())
    return len(chats), total_turns


async def _autosave(app: web.Application, session: ServerSession) -> None:
    opts = app["adapter_options"]
    chat_count, turns = _session_chat_summary(session)
    try:
        now = datetime.now(timezone.utc)
        body = (
            f"Mirror session closed (id={session.session_id}).\n"
            f"Chats: {chat_count}  |  Turns (all chats): {turns}  |  "
            f"Compactions: {session.compact_count}  |  Memory saves: {session.memory_saves}\n"
            f"Close reason: normal"
        )
        append_log_entry(
            header=f"## [session_end] Session {session.session_id[:8]} closed {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            body=body,
            log_dir=log_dir("sessions"),
            date=now,
            idempotency_probe=f"id={session.session_id}",
        )
    except Exception:
        log.exception("logs/sessions [session_end] append failed for %s", session.session_id)
    # Flush every chat to its own sessions/chats/<chat_id>.json (open +
    # archived) so multi-chat state survives a restart. `skip_empty` stays
    # False here — archive state belongs on disk for a chat that was never
    # typed in. This does NOT stamp a close time: `save_chat` reads `ended_at`
    # off the last message, so a conversation is dated by when it last changed
    # rather than by when the connection carrying it happened to end.
    # Both halves write files and one of them writes SQLite, so both go to a
    # thread. On the loop they were the largest measured source of blocking in
    # the backend (2026-09-02): a long conversation is a lot of bytes to
    # serialise and a lot of rows to index, and while that ran nothing else in
    # the process moved, including the health probe the supervisor kills the
    # backend for not answering.
    #
    # `WorkIndex` is built for this: its connections are thread-local for
    # exactly this reason and its own docstring says so.
    def _persist_and_index() -> tuple[int, int]:
        saved = chat_store.persist_session_chats(
            session, model=getattr(opts, "model", "") or "",
        )
        # Must follow persist so the files exist on disk. Kept in the same
        # thread hop rather than two: they are one act, and splitting them
        # would put the loop back between a write and the index of it.
        return saved, chat_store.index_session_chats(session)

    try:
        n, indexed = await asyncio.to_thread(_persist_and_index)
        if n:
            log.info("autosaved %d chat(s) for session %s", n, session.session_id)
        if indexed:
            log.info("recall-indexed %d chat(s) for session %s", indexed, session.session_id)
    except Exception:
        log.exception("chat-store autosave failed for %s", session.session_id)
