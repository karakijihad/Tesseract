"""``/api/health`` — backend liveness probe.

Liveness, not readiness. The probe returns HTTP 200 as long as the
process is responding; ``state`` in the body surfaces boot progress
through a three-state machine:

* ``initializing`` — aiohttp has bound the listener but ``_init_background``
  is still building substrates (tool registry, voice runtime, telegram
  bridge, etc.). Routes that depend on a substrate may still return 503.
* ``recovering`` — AU-2 RecoveryManager is reconciling boot-time state.
  Set just before ``rm.run()`` and cleared after it completes.
* ``ready`` — everything is wired. Default when recovery never ran.

Splitting liveness from readiness matches the kubernetes convention:
the supervisor cares about *am I responding* (always 200 once the
listener is up); the dashboard / observability cares about *am I ready*
(reads the body's ``state`` field).

Boot model lives in ``app.py::_on_startup`` + ``_init_background`` —
``_on_startup`` finishes in <1s so aiohttp binds the port immediately;
the heavy chain runs async without blocking the listener.
"""

from __future__ import annotations

import time

from aiohttp import web


async def health(request: web.Request) -> web.Response:
    started = request.app.get("started_at")
    uptime = round(time.monotonic() - started, 3) if started is not None else 0.0
    state = request.app.get("recovery_state") or "ready"
    body: dict[str, object] = {
        "status": "ok" if state == "ready" else state,
        "state": state,
        "uptime_seconds": uptime,
    }
    return web.json_response(body)
