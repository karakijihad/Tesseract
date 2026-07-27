"""Audit-1 M-2 — Mirror websocket bridge for controller transcripts.

Until this module landed, TC-9 parity proved that two raw TCP clients
attached to the controller daemon saw the same typed transcript stream
— but no test (and no production code) ever attached *through the
Mirror surface*. Operators on the Mirror frontend who wanted live
controller output were stuck reading the CLI session in another window.

This handler is the missing live boundary. A frontend that opens
``GET /ws/controller/{session_id}`` gets a Mirror-typed envelope for
every controller transcript event — replay (events buffered before the
attach) first, then live pushes streamed in real time. Envelope shape::

    {
      "type": "controller_event",
      "category": "controller",
      "session_id": "<sid>",
      "timestamp": "...",
      "data": <typed transcript_event dict>
    }

The ``data`` payload is the controller's typed event verbatim (kind +
session_id + ts + origin + per-kind fields) — same payload the CLI
observer receives. The Mirror frontend can render it without any
controller-specific decoding because the typed event vocabulary is
already documented in ``tars_controller/events.py``.

The handler is intentionally narrow: it forwards transcript events
and nothing else. ``session_status`` / ``reload_complete`` / ``ack``
pushes are ignored — the Mirror has its own out-of-band signals for
those domains. Only ``transcript_event`` carries chat / tool / PTY
content the operator needs to see live.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

from tesseract.mirror.server.envelope import make_envelope
from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClient,
    ControllerClientError,
)

log = logging.getLogger(__name__)


# A ``controller_client_factory`` may be injected on the app under the
# key below so tests can supply a stub without spinning up a real
# daemon. Production leaves this unset and the handler uses
# ``ControllerClient.connect`` directly.
APP_FACTORY_KEY = "controller_client_factory"


# Protocol-style hint — anything quack-typed to ``ControllerClient`` works.
ControllerClientFactory = Callable[[], Awaitable[Any]]


async def controller_ws_handler(request: web.Request) -> web.WebSocketResponse:
    """``GET /ws/controller/{session_id}`` — Mirror observer bridge.

    Each connection opens a fresh ``ControllerClient``, attaches as an
    observer (so it doesn't compete with an interactive CLI client for
    typing), forwards the replay buffer first, then streams live
    transcript events until either side closes.

    Failure modes are surfaced explicitly to the operator instead of
    silently aborting:

    * Daemon not running / unreachable → ``controller_error`` envelope
      with the underlying ``ControllerClientError`` message, then the
      WS closes. The frontend can render a "controller offline" toast.
    * Daemon disconnects mid-stream → ``controller_disconnected``
      envelope, then the WS closes. The frontend can offer a
      reconnect button.
    """
    session_id = request.match_info["session_id"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    factory: ControllerClientFactory | None = request.app.get(APP_FACTORY_KEY)
    try:
        client = await (factory() if factory else ControllerClient.connect())
    except ControllerClientError as exc:
        await _send(ws, _error_envelope(session_id, "connect_failed", str(exc)))
        await ws.close()
        return ws
    except Exception as exc:  # noqa: BLE001 — unexpected, but must not leak
        log.exception(
            "controller_ws: client factory raised for session %s", session_id
        )
        await _send(
            ws,
            _error_envelope(session_id, "connect_failed", f"{type(exc).__name__}: {exc}"),
        )
        await ws.close()
        return ws

    try:
        try:
            attached = await client.attach(
                session_id, mode="observer", from_offset=0
            )
        except ControllerClientError as exc:
            await _send(
                ws, _error_envelope(session_id, "attach_failed", str(exc))
            )
            return ws

        for replay_event in attached.get("replay_events") or []:
            if not isinstance(replay_event, dict):
                continue
            await _send(ws, _controller_envelope(session_id, replay_event))
            if ws.closed:
                return ws

        # Tee two coroutines: forward controller pushes to the WS, and
        # watch the WS for client-initiated close. First to complete
        # ends the bridge cleanly.
        forward_task = asyncio.create_task(
            _forward_pushes(client, ws, session_id),
            name=f"controller_ws_forward:{session_id}",
        )
        ws_watcher_task = asyncio.create_task(
            _watch_ws_close(ws),
            name=f"controller_ws_watch:{session_id}",
        )
        try:
            _, pending = await asyncio.wait(
                {forward_task, ws_watcher_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception:
            log.exception(
                "controller_ws: forward loop raised for session %s",
                session_id,
            )
    finally:
        try:
            await client.close()
        except Exception:
            log.exception(
                "controller_ws: client close raised for session %s",
                session_id,
            )
        if not ws.closed:
            await ws.close()
    return ws


async def _forward_pushes(
    client: Any, ws: web.WebSocketResponse, session_id: str,
) -> None:
    """Consume pushes from the controller client and forward typed
    transcript events to the WS. Surfaces controller-side disconnect
    with a sentinel envelope so the frontend can react."""
    async for push in client.pushes():
        if ws.closed:
            return
        event_kind = push.get("event")
        if event_kind == "_disconnected":
            await _send(
                ws,
                _error_envelope(
                    session_id, "controller_disconnected",
                    "controller closed the IPC connection",
                ),
            )
            return
        if event_kind != "transcript_event":
            # Other push kinds (ack, session_status, reload_complete) are
            # not part of the transcript surface; the Mirror has other
            # paths for those signals.
            continue
        transcript_event = push.get("transcript_event")
        if not isinstance(transcript_event, dict):
            continue
        await _send(ws, _controller_envelope(session_id, transcript_event))


async def _watch_ws_close(ws: web.WebSocketResponse) -> None:
    """Block until the Mirror WS peer closes (or sends an error). The
    bridge doesn't accept client-to-controller traffic — observers are
    read-only — so any incoming frame just keeps the watcher alive."""
    async for msg in ws:
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
            return
        # Any other frame type is ignored. Observers don't talk back.


def _controller_envelope(session_id: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return make_envelope("controller_event", "controller", session_id, event_dict)


def _error_envelope(
    session_id: str, code: str, detail: str,
) -> dict[str, Any]:
    return make_envelope(
        "controller_error",
        "controller",
        session_id,
        {"code": code, "detail": detail},
    )


async def _send(ws: web.WebSocketResponse, envelope: dict[str, Any]) -> None:
    if ws.closed:
        return
    try:
        await ws.send_json(envelope)
    except (ConnectionResetError, RuntimeError):
        # Peer went away mid-send. Caller's outer loop sees ws.closed.
        return


__all__ = [
    "APP_FACTORY_KEY",
    "controller_ws_handler",
]
