"""Backend log forwarder — pipes server-side errors into the pulse feed.

Installs a `logging.Handler` on the root logger that broadcasts a
``log_error`` envelope to all active Mirror sessions. The operator sees
backend failures (TTS provider errors, scheduler exceptions, adapter
blow-ups) in the pulse panel without tailing the server terminal.

Admits every record at ``ERROR`` or above, plus ``WARNING`` from the narrow
``_ELEVATED_LOGGERS`` list — so a module with something the operator must see
does not have to claim ERROR to be heard. The envelope carries the level and
the frontend classifies on it, keeping the pulse's errors-only view a
real-failure view.

The handler tolerates being called from any thread — sync log calls in a
background worker still reach the asyncio loop via
``loop.call_soon_threadsafe``. When no loop is running yet (boot-time
errors before ``_on_startup``) the record is logged to stderr and dropped
from the wire — there is no session to deliver to anyway.

De-dupes on logger name + message + 1-second window so a tight retry loop
doesn't spam the pulse.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from aiohttp import web

from tesseract.mirror.server.envelope import make_log_error
from tesseract.mirror.server.session import send_envelope

_DEDUPE_WINDOW_SECONDS = 1.0
_DEDUPE_RING_CAP = 64

# Loggers we never forward — they fire at error level for non-actionable
# reasons (peer disconnect race, scheduler tick collisions). Add narrowly,
# never broadly.
_SUPPRESSED_LOGGERS: frozenset[str] = frozenset()

# Loggers whose WARNING records also reach the pulse. This exists so a module
# with something the operator must see does not have to claim ERROR to be
# heard — severity should describe the event, not choose the channel. A stale
# worker is a true operational fact and a false error, and logging it as the
# latter put it in the pulse's `errorsOnly` view beside real crashes. Add
# narrowly, never broadly, and only for loggers whose warnings are all
# operator-facing.
_ELEVATED_LOGGERS: frozenset[str] = frozenset(
    {"tesseract.scheduler.tasks.worker_liveness"}
)


class MirrorLogHandler(logging.Handler):
    """Forwards records >= ERROR to every live ServerSession as `log_error`,
    plus WARNING records from `_ELEVATED_LOGGERS`.

    One handler per Mirror process. Installed on the root logger in
    ``_on_startup``; uninstalled in ``_on_shutdown`` so test harnesses
    don't accumulate handlers across app rebuilds."""

    def __init__(self, app: web.Application) -> None:
        # WARNING, so `_ELEVATED_LOGGERS` records reach `emit` at all; every
        # other logger is filtered back up to ERROR there.
        super().__init__(level=logging.WARNING)
        self._app = app
        self._loop: asyncio.AbstractEventLoop | None = None
        self._recent: deque[tuple[float, str]] = deque(maxlen=_DEDUPE_RING_CAP)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the loop reference at startup so cross-thread log calls
        from the scheduler / observer workers can hop back onto it."""
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        # First, before any other work. The handler sits on the ROOT logger at
        # WARNING so elevated records can reach here at all, which means every
        # warning in the process now enters emit — httpx, aiohttp, onnxruntime.
        # Reject what is not ours before touching the suppression lookup or the
        # dedupe ring.
        #
        # The WARNING floor is asserted here rather than left to the handler's
        # level: relying on that made the elevation check unbounded downward,
        # so a future change lowering the handler level would have started
        # forwarding INFO and DEBUG from elevated loggers. emit decides for
        # itself what it admits.
        if record.levelno < logging.WARNING:
            return
        if record.levelno < logging.ERROR and record.name not in _ELEVATED_LOGGERS:
            return
        if record.name in _SUPPRESSED_LOGGERS:
            return
        try:
            message = record.getMessage()
        except Exception:
            return  # malformed log call — never let logging break logging
        key = f"{record.name}:{message[:120]}"
        now = time.monotonic()
        for ts, prior in self._recent:
            if prior == key and (now - ts) < _DEDUPE_WINDOW_SECONDS:
                return
        self._recent.append((now, key))

        exc_type: str | None = None
        exc_message: str | None = None
        if record.exc_info and record.exc_info[0] is not None:
            exc_type = record.exc_info[0].__name__
            exc_message = str(record.exc_info[1]) if record.exc_info[1] else None

        payload = {
            "level": record.levelname,
            "logger_name": record.name,
            "message": message,
            "exc_type": exc_type,
            "exc_message": exc_message,
        }
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_broadcast, payload)
        except RuntimeError:
            return

    def _schedule_broadcast(self, payload: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        loop.create_task(_broadcast(self._app, payload))


async def _broadcast(app: web.Application, payload: dict[str, Any]) -> None:
    sessions = list(app.get("server_sessions", {}).values())
    if not sessions:
        return
    for session in sessions:
        try:
            envelope = make_log_error(
                session.session_id,
                level=payload["level"],
                logger_name=payload["logger_name"],
                message=payload["message"],
                exc_type=payload.get("exc_type"),
                exc_message=payload.get("exc_message"),
            )
            await send_envelope(session, envelope)
        except Exception:
            # Forwarding must never raise back into the logging pipeline —
            # that loops infinitely. Drop quietly.
            continue


def install_log_forwarder(app: web.Application, loop: asyncio.AbstractEventLoop) -> MirrorLogHandler:
    handler = MirrorLogHandler(app)
    handler.bind_loop(loop)
    logging.getLogger().addHandler(handler)
    return handler


def uninstall_log_forwarder(handler: MirrorLogHandler | None) -> None:
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
