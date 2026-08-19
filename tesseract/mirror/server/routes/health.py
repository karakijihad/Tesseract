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

``warm`` is a fourth, orthogonal fact: whether every substrate the operator
can immediately touch has been prepared. ``state`` goes ``ready`` as soon as
recovery completes, with most substrates still building — so it cannot answer
"can I use this yet". The launch splash waits on ``warm`` before revealing the
cockpit, which is what makes the app warm when the window opens rather than
warming while it is used.

It fires at the warm line — after the last ``blocks_window: true`` layer in
``config/boot.yaml``, with the layers below it still running behind the
window. That is the line's whole purpose: the autonomy kernel, the config
watcher and the outbound MCP clients each declare what the app does before
they are ready, so none of them is worth making the operator wait for. It is
also set in ``_init_background``'s ``finally`` as a safety net — a boot that
crashed before reaching the line is as warm as the process will get, and the
window must still appear.

Splitting liveness from readiness matches the kubernetes convention:
the supervisor cares about *am I responding* (always 200 once the
listener is up); the dashboard / observability cares about *am I ready*
(reads the body's ``state`` field).

Boot model lives in ``config/boot.yaml`` and ``app.py::_on_startup`` +
``_init_background`` — ``_on_startup`` reads and validates the graph, then
finishes in <1s so aiohttp binds the port immediately; walking the graph runs
async without blocking the listener.
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
        "warm": bool(request.app.get("warm")),
        "uptime_seconds": uptime,
    }
    return web.json_response(body)
