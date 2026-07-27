"""Observer consent + activation endpoints.

Backend handlers for the four `/api/observer/*` paths that
`tesseract/mirror/src/stores/observer.ts` already calls. Phase 12 left
them returning 404; Phase 3 wires them.

State lives on the aiohttp `app` and is in-memory only:

- ``app["observer_state"]: str`` — ``off`` | ``armed`` | ``observing``.
- ``app["observer_consented_panes"]: set[str]`` — pane_ids the operator
  granted observation consent to (set via the WS ``observer_pane_ack``
  message; cleared on disarm, pane close, and WS disconnect).

Single-operator local deployment — one observer instance, one consent
set, no per-WS sharding. Matches the `_shared/pty-consent.md` decision
that consent is never persisted.
"""

from __future__ import annotations

import logging

from aiohttp import web

from tesseract.brain.memory_suggestion import MemorySuggestion, to_envelope_data
from tesseract.mirror.server.envelope import make_envelope

log = logging.getLogger(__name__)


_VALID_STATES = {"off", "armed", "observing"}


def _no_observer() -> web.Response:
    return web.json_response({"error": "observer unavailable"}, status=503)


def _make_emit_fn(server_session):
    async def emit(suggestion: MemorySuggestion) -> None:
        ws = server_session.ws
        if ws.closed:
            return
        env = make_envelope(
            "memory_suggestion", "background", server_session.session_id,
            to_envelope_data(suggestion),
        )
        try:
            await ws.send_json(env)
        except ConnectionResetError:
            log.debug("observer: ws closed mid-emit for %s", server_session.session_id)

    return emit


def _attach_to_active_sessions(app: web.Application) -> int:
    subscriber = app.get("observer_subscriber")
    if subscriber is None:
        return 0
    sessions = list(app.get("server_sessions", {}).values())
    if not sessions:
        return 0
    # Single-operator deployment — one WS session in practice; attach to
    # the newest if more than one exists.
    target = sessions[-1]
    target.chat_session.attach_observer_subscriber(subscriber)
    subscriber.attach(target.chat_session, _make_emit_fn(target))
    return 1


async def _detach_subscriber(app: web.Application) -> None:
    subscriber = app.get("observer_subscriber")
    if subscriber is None:
        return
    for session in app.get("server_sessions", {}).values():
        try:
            session.chat_session.detach_observer_subscriber()
        except Exception:
            log.exception("detach_observer_subscriber failed")
    try:
        await subscriber.detach()
    except Exception:
        log.exception("observer_subscriber.detach failed")
    # Cancel any in-flight PTY-push tasks scheduled by pty_manager. These
    # escape the subscriber's own _tasks set because they are scheduled
    # from a different call site.
    pty_tasks = app.get("observer_pty_tasks")
    if pty_tasks:
        import asyncio as _asyncio
        tasks = list(pty_tasks)
        pty_tasks.clear()
        for t in tasks:
            if not t.done():
                t.cancel()
        try:
            await _asyncio.wait_for(
                _asyncio.gather(*tasks, return_exceptions=True),
                timeout=2.0,
            )
        except _asyncio.TimeoutError:
            log.warning("%d PTY-push task(s) still pending after detach timeout", len(tasks))


async def arm(request: web.Request) -> web.Response:
    if request.app.get("observer") is None:
        return _no_observer()
    # Idempotent: if a prior arm left a subscriber attached (double-POST,
    # reconnect race, pre-existing state), tear it down first so the fresh
    # attach below points at the current session with no stale references.
    await _detach_subscriber(request.app)
    request.app["observer_state"] = "armed"
    attached = _attach_to_active_sessions(request.app)
    # Phase 6 (terminal-control 2026-05-16) — observer-always-on. Bulk-
    # grant consent for every live pane so re-arming immediately resumes
    # PTY observation without waiting for the operator to spawn fresh
    # panes. New panes spawned while armed also auto-grant via
    # PTYManager._maybe_auto_grant_consent.
    pty = request.app.get("pty_manager")
    granted = pty.grant_consent_for_all_live() if pty is not None else 0
    if granted:
        request.app["observer_state"] = "observing"
    log.info(
        "observer: armed (subscriber attached to %d session(s), "
        "%d live pane(s) auto-consented)",
        attached, granted,
    )
    return web.json_response({"state": request.app["observer_state"]})


async def disarm(request: web.Request) -> web.Response:
    observer = request.app.get("observer")
    if observer is None:
        return _no_observer()
    await _detach_subscriber(request.app)
    observer.reset()
    request.app["observer_state"] = "off"
    request.app["observer_consented_panes"] = set()
    log.info("observer: disarmed (subscriber detached, transcript + PTY buffer cleared, consents cleared)")
    return web.json_response({"state": "off"})


async def status(request: web.Request) -> web.Response:
    if "observer_state" not in request.app:
        # `app["observer_state"]` is initialized in `app.py:_on_startup`
        # — a missing key here means the app wasn't bootstrapped, which is
        # a programmer error. Surface 500 instead of silently coercing "off".
        return web.json_response({"error": "observer_state not initialized"}, status=500)
    state = request.app["observer_state"]
    if state not in _VALID_STATES:
        return web.json_response(
            {"error": f"invalid observer_state {state!r}", "valid": sorted(_VALID_STATES)},
            status=500,
        )
    return web.json_response({"state": state})


