"""WS connection lifecycle — handshake, background pumps, teardown, autosave."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from tesseract.brain.boot import SESSIONS_DIR
from tesseract.paths import TESSERACT_HOME
from tesseract.brain.session_store import (
    default_session_name,
    save_session,
)
from tesseract.memory.log_notes import append_log_entry
from tesseract.mirror.server.envelope import (
    make_cost_state,
    make_entity_signals,
    make_envelope,
)
from tesseract.mirror.server import chat_store, spawn_wake
from tesseract.mirror.server.chat_lifecycle import _open_chats_payload
from tesseract.mirror.server.session import (
    ChatInfraNotReady,
    ServerSession,
    cleanup_session,
    create_server_session,
    send_envelope,
)
from tesseract.mirror.server.voice_io import _accumulate_voice_pcm, note_voice_audio

log = logging.getLogger(__name__)

# Frontend `IntensitySignals.BACKEND_STALENESS_MS = 3000`. Pumping every 2.0s
# keeps the freshness window with a 1s jitter margin.
ENTITY_SIGNALS_PUMP_INTERVAL_S = 2.0


async def _attach_observer_subscriber_if_armed(app: web.Application, session: ServerSession) -> None:
    """Attach the boot-armed observer subscriber to a freshly-connected WS.

    `arm()` in `routes/observer_consent.py` does the canonical attach via
    `_attach_to_active_sessions`, but it can only see sessions that exist
    when it runs. When observer is armed at boot (`app.py:_on_startup`),
    no WS is connected yet — the subscriber sits idle until something
    re-arms. This wires a fresh WS to the already-armed subscriber so the
    operator gets observer suggestions on first connect without having to
    click `disarm/arm`.
    """
    if app.get("observer_state") not in {"armed", "observing"}:
        return
    subscriber = app.get("observer_subscriber")
    if subscriber is None:
        return
    try:
        from tesseract.mirror.server.routes.observer_consent import (
            _detach_subscriber,
            _make_emit_fn,
        )
        # Reviewer follow-up: `is_active` flips True on any prior `attach()`
        # and stays True if the previous WS closed without a clean disarm
        # (e.g. browser refresh). Skipping on `is_active` would silently
        # leave the subscriber pointing at the dead session's emit_fn —
        # the new session would receive no suggestions. Detach first,
        # mirroring `routes/observer_consent.arm()`.
        if subscriber.is_active:
            await _detach_subscriber(app)
        session.chat_session.attach_observer_subscriber(subscriber)
        subscriber.attach(session.chat_session, _make_emit_fn(session))
    except Exception:
        log.exception("observer subscriber attach-on-connect failed")


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
    # 2026-05-15 — any operator WS connection counts as a renderer for
    # agent-spawned PTY panes. Previously `primary_ws` was only set when
    # the operator dispatched a `terminal_*` message (i.e. while on the
    # Terminal tab); an agent-spawned viewer pane (`start_controller_session`
    # / boot-time reattach) would then refuse with `no_primary_ws` if the
    # operator was on Chat / Workspace / any other tab. Promote on connect
    # so the viewer-pane open works whenever a Mirror tab is open.
    # `pty_manager.cleanup_for_ws` clears the ref on disconnect.
    request.app["primary_ws"] = ws
    await send_envelope(session, make_envelope(
        "session_created",
        "session",
        session.session_id,
        {
            "session_id": session.session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            # mirror-multi-chat inc.B — the frontend seeds its active chat
            # slice from this so the default slice key matches the backend's
            # chat_id; turn-scoped envelopes (inc.A) then route by exact id.
            "active_chat_id": session.active_chat_id,
            # P3 — the open-chat list (newest-first, with titles) so the tab
            # strip rehydrates on (re)connect and survives a page reload.
            "chats": _open_chats_payload(session),
        },
    ))
    # Owner request 2026-04-29 — observer arms by default at boot
    # (`app.py:_on_startup`). Subscriber attachment normally happens in
    # `/api/observer/arm`, but at boot there's no WS yet. The first WS
    # connection after a boot-armed observer needs to attach itself.
    await _attach_observer_subscriber_if_armed(request.app, session)
    await _emit_cost_state(request.app, session)
    await _emit_entity_signals(request.app, session)
    session.entity_signals_task = _spawn_tracked(
        request.app,
        _entity_signals_pump(request.app, session),
        f"entity_signals_pump:{session.session_id}",
    )
    session.surface_events_task = _spawn_tracked(
        request.app,
        _surface_events_pump(request.app, session),
        f"surface_events_pump:{session.session_id}",
    )
    session.activity_events_task = _spawn_tracked(
        request.app,
        _activity_events_pump(request.app, session),
        f"activity_events_pump:{session.session_id}",
    )
    # Spawn push Stage 2 — wrap every open chat's spawn completion notifier so a
    # background spawn finishing while that chat is idle starts a proactive turn.
    spawn_wake.install(request.app, session)
    # Lazy: `_dispatch` stays in ws.py (the slim router), which re-exports
    # this module's public names — a module-level import here would cycle
    # with that re-export.
    from tesseract.mirror.server import ws as _ws
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _ws._dispatch(request.app, session, msg.data)
            elif msg.type == WSMsgType.BINARY:
                _accumulate_voice_pcm(session, msg.data)
                # SC-5 — first PCM frame of an utterance drives the voice loop
                # to `listening` (turn-gated, deduped — cheap on later frames).
                await note_voice_audio(
                    session, turn_active=session.current_turn_task is not None
                )
            elif msg.type == WSMsgType.ERROR:
                log.warning("ws error: %s", ws.exception())
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING):
                break
    finally:
        await _cancel_entity_signals_pump(session)
        await _cancel_surface_events_pump(session)
        await _cancel_activity_events_pump(session)
        try:
            await request.app["pty_manager"].cleanup_for_ws(ws)
        finally:
            try:
                await _autosave(request.app, session)
            finally:
                cleanup_session(request.app, session)
    return ws


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
    task = session.entity_signals_task
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("entity_signals pump exit raised for %s", session.session_id)
    session.entity_signals_task = None


async def _surface_events_pump(app: web.Application, session: ServerSession) -> None:
    # Surfaces are view-scoped but operator-global — a surface a tool spawns
    # from any session must light up on the Mirror canvas. Filter by channel
    # only; the frontend re-keys the `{kind, channel: "surface", …}` envelope
    # to a `category: "canvas"` Envelope and routes it to the surfaces store.
    from tesseract.orchestrator.background_event_bus import get_background_bus
    from tesseract.orchestrator.surfaces.events import CHANNEL as SURFACE_CHANNEL

    bus = get_background_bus()
    _replay, queue = bus.subscribe()
    # Surfaces have a REST catch-up path: the frontend
    # `GET /api/surfaces/{view}` on canvas mount fetches the current persisted
    # state. So we deliberately DROP the ring-buffer replay here and forward
    # only live events — replaying stale `surface_created`/`_closed` deltas
    # could otherwise re-insert a ghost card the REST hydrate already settled.
    try:
        while not session.ws.closed:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                raise
            if not _envelope_for_channel(event.data, SURFACE_CHANNEL):
                continue
            if session.ws.closed:
                return
            try:
                await send_envelope(session, event.data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("surface forward failed for %s", session.session_id)
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(queue)


async def _cancel_surface_events_pump(session: ServerSession) -> None:
    task = session.surface_events_task
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("surface pump exit raised for %s", session.session_id)
    session.surface_events_task = None


async def _activity_events_pump(app: web.Application, session: ServerSession) -> None:
    # AS-1 — Unified Activity stream. Operator-global like surfaces:
    # delegates (this process), lanes + controller sessions (pushed from the
    # controller daemon into this process's registry+bus). Filter by channel
    # only; the frontend re-keys the `{kind, channel: "activity", …}` envelope
    # into its activity store. Replay is DROPPED — `GET /api/activity` is the
    # catch-up path, so replaying stale deltas could re-insert a ghost the
    # REST hydrate already settled (same reasoning as the surface pump).
    from tesseract.orchestrator.background_event_bus import get_background_bus
    from tesseract.orchestrator.activity import CHANNEL as ACTIVITY_CHANNEL

    bus = get_background_bus()
    _replay, queue = bus.subscribe()
    try:
        while not session.ws.closed:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                raise
            if not _envelope_for_channel(event.data, ACTIVITY_CHANNEL):
                continue
            if session.ws.closed:
                return
            try:
                await send_envelope(session, event.data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("activity forward failed for %s", session.session_id)
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(queue)


async def _cancel_activity_events_pump(session: ServerSession) -> None:
    task = session.activity_events_task
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("activity pump exit raised for %s", session.session_id)
    session.activity_events_task = None


def _envelope_for_channel(payload: Any, channel: str) -> bool:
    if not isinstance(payload, dict):
        return False
    return payload.get("channel") == channel


def _session_chat_summary(session: ServerSession) -> tuple[int, int]:
    """Return ``(chat_count, total_turns)`` across ALL chats in the session.

    The legacy close-log counted only the active chat's turns; with multi-chat
    that under-reports a session that ran background chats. Turns = user turns
    (``len(history) // 2``) summed over every open + archived chat.
    """
    chats = getattr(session, "chats", None) or {}
    total_turns = sum(len(getattr(cs, "history", []) or []) // 2 for cs in chats.values())
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
            log_dir=TESSERACT_HOME / "logs" / "sessions",
            date=now,
            idempotency_probe=f"id={session.session_id}",
        )
    except Exception:
        log.exception("logs/sessions [session_end] append failed for %s", session.session_id)
    # mirror-multi-chat P1 — flush every chat to its own
    # sessions/chats/<chat_id>.json (open + archived) so multi-chat state
    # survives a restart. Independent of the legacy active-chat save below;
    # best-effort so a chat-store fault never blocks the session autosave.
    try:
        n = chat_store.persist_session_chats(session)
        if n:
            log.info("autosaved %d chat(s) for session %s", n, session.session_id)
        # Index every chat into the work-index for recall — not just the active
        # one. Must follow persist so the files exist on disk.
        indexed = chat_store.index_session_chats(session)
        if indexed:
            log.info("recall-indexed %d chat(s) for session %s", indexed, session.session_id)
    except Exception:
        log.exception("chat-store autosave failed for %s", session.session_id)
    if opts is None:
        return
    if not session.chat_session.history:
        return
    name = session.save_name or default_session_name()
    try:
        # index_work=False: each chat is recall-indexed above by its own
        # chats/<chat_id>.json. This legacy snapshot of the active chat is kept
        # only for back-compat (resume, the session drawer's disk glob); indexing
        # it too would double-index the active chat's recall.
        save_session(
            SESSIONS_DIR,
            name,
            opts.model,
            session.started_at,
            list(session.chat_session.history),
            index_work=False,
        )
        log.info("autosaved session %s to %s.json", session.session_id, name)
    except Exception:
        log.exception("autosave failed for %s", session.session_id)
